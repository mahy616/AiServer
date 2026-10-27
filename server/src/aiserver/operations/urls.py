"""Credential-free HTTPS origin validation shared with local configuration tools."""
from urllib.parse import urlsplit


def public_url(value):
    if value is None or value == "": return ""
    if not isinstance(value,str): raise ValueError("公网入口须为HTTPS地址。")
    value = value.strip().rstrip("/")
    try:
        uri = urlsplit(value)
        valid = (len(value)<=2048 and not any(char.isspace() or ord(char)<32 for char in value)
            and uri.scheme=="https" and uri.hostname and uri.username is None and uri.password is None
            and uri.path=="" and not uri.query and not uri.fragment and uri.port != 0)
    except ValueError:
        valid = False
    if not valid: raise ValueError("公网入口须为不含凭据、路径或查询参数的HTTPS地址。")
    return value
