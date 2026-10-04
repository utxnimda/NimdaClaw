"""Safe network diagnostics without URLs, proxy credentials or remote bodies."""
from __future__ import annotations

from http.client import HTTPException, IncompleteRead
import socket
import ssl
from urllib.error import URLError


def failure_reason(error: BaseException) -> str:
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLEOFError):
        return "TLS 握手或传输被中断（SSLEOFError）"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS 证书校验失败（SSLCertVerificationError）"
    if isinstance(reason, ssl.SSLError):
        return "TLS 安全连接失败（SSLError）"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "请求超时（TimeoutError）"
    if isinstance(reason, socket.gaierror):
        return "域名解析失败（gaierror）"
    if isinstance(reason, ConnectionRefusedError):
        return "连接被拒绝（ConnectionRefusedError）"
    if isinstance(reason, ConnectionResetError):
        return "连接被重置（ConnectionResetError）"
    if isinstance(reason, IncompleteRead):
        return "响应连接提前结束（IncompleteRead）"
    if isinstance(reason, HTTPException):
        return "HTTP 连接协议异常（HTTPException）"
    if isinstance(reason, ConnectionError):
        return "网络连接中断（ConnectionError）"
    if isinstance(reason, str) and "timed out" in reason.lower():
        return "请求超时（URLError）"
    return "网络连接不可用（URLError）"
